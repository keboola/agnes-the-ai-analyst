"""unified_search — fan-out over chunks + knowledge + tables + metrics + glossary (K2, #1108)."""

from __future__ import annotations

from unittest.mock import patch

TABLES = [
    {"id": "t_orders", "name": "orders", "description": "customer orders and revenue", "columns_json": None},
    {"id": "t_web", "name": "web_sessions", "description": "web analytics sessions", "columns_json": None},
]

METRICS = [
    {
        "id": "finance/mrr",
        "name": "mrr",
        "display_name": "Monthly Recurring Revenue",
        "description": "normalized monthly subscription revenue",
        "synonyms": ["MRR", "recurring revenue"],
        "category": "finance",
    },
    {
        "id": "product/wau",
        "name": "wau",
        "display_name": "Weekly Active Users",
        "description": "distinct users with a session in 7 days",
        "synonyms": ["WAU"],
        "category": "product",
    },
]


def _fake_chunks(corpus_ids, query, k=10):
    if not corpus_ids:
        return []
    return [
        {
            "chunk_id": "ch1",
            "corpus_id": "c1",
            "file_id": "f1",
            "filename": "billing.md",
            "ordinal": 0,
            "section_path": None,
            "text": "invoices are monthly",
            "score": 0.9,
            "confidence": "high",
        }
    ]


def _fake_knowledge(query, **kw):
    if not kw.get("granted_domains") and not kw.get("user_groups"):
        return []
    return [
        {
            "id": "ki1",
            "title": "Billing policy",
            "content": "We invoice monthly in EUR.",
            "domain": "finance",
        }
    ]


def _no_glossary(query, limit=10):
    """Default glossary mock — empty, so the base tests keep their 3-type shape."""
    return []


def _fake_glossary(query, limit=10):
    return [{"id": "g_mrr", "term": "Recurring revenue", "definition": "Revenue that recurs each period."}]


def test_merges_all_five_sources():
    from src.search.unified import unified_search

    with (
        patch("src.search.unified._chunk_search", _fake_chunks),
        patch("src.search.unified._knowledge_search", _fake_knowledge),
        patch("src.search.unified._glossary_search", _fake_glossary),
    ):
        hits = unified_search(
            "invoices orders revenue",
            corpus_ids=["c1"],
            user_groups=["g1"],
            granted_domains=["d1"],
            tables=TABLES,
            metrics=METRICS,
            k=10,
        )
    types = {h["type"] for h in hits}
    assert types == {"chunk", "knowledge", "table", "metric", "glossary"}
    table_hit = next(h for h in hits if h["type"] == "table")
    assert table_hit["table_id"] == "t_orders"
    assert "agnes query" in table_hit["pivot_hint"]
    metric_hit = next(h for h in hits if h["type"] == "metric")
    assert metric_hit["id"] == "finance/mrr"
    assert metric_hit["display_name"] == "Monthly Recurring Revenue"
    glossary_hit = next(h for h in hits if h["type"] == "glossary")
    assert glossary_hit["term"] == "Recurring revenue"


def test_glossary_is_public_independent_of_grants():
    """Glossary is fetched inside (no RBAC), so it appears even with zero grants
    where the knowledge source is fail-closed."""
    from src.search.unified import unified_search

    with (
        patch("src.search.unified._chunk_search", _fake_chunks),
        patch("src.search.unified._knowledge_search", _fake_knowledge),
        patch("src.search.unified._glossary_search", _fake_glossary),
    ):
        hits = unified_search("revenue", corpus_ids=[], user_groups=[], granted_domains=[], tables=[], metrics=[], k=10)
    types = {h["type"] for h in hits}
    assert "glossary" in types  # public
    assert "knowledge" not in types  # fail-closed with zero grants


def test_metrics_absent_when_not_passed():
    """Metric RBAC pre-filter is the caller's job — an empty/omitted metrics list
    yields no metric hits."""
    from src.search.unified import unified_search

    with (
        patch("src.search.unified._chunk_search", _fake_chunks),
        patch("src.search.unified._knowledge_search", _fake_knowledge),
        patch("src.search.unified._glossary_search", _no_glossary),
    ):
        hits = unified_search("revenue", corpus_ids=[], user_groups=[], granted_domains=[], tables=[], metrics=[], k=10)
        # omitted entirely (default None) behaves the same
        hits2 = unified_search("revenue", corpus_ids=[], user_groups=[], granted_domains=[], tables=[], k=10)
    assert not any(h["type"] == "metric" for h in hits)
    assert not any(h["type"] == "metric" for h in hits2)


def test_fail_closed_per_source():
    from src.search.unified import unified_search

    with (
        patch("src.search.unified._chunk_search", _fake_chunks),
        patch("src.search.unified._knowledge_search", _fake_knowledge),
        patch("src.search.unified._glossary_search", _no_glossary),
    ):
        hits = unified_search(
            "invoices", corpus_ids=[], user_groups=[], granted_domains=[], tables=[], metrics=[], k=10
        )
    assert hits == []


def test_blank_query_returns_empty():
    from src.search.unified import unified_search

    assert unified_search("  ", corpus_ids=["c1"], user_groups=["g"], granted_domains=["d"], tables=TABLES) == []


def test_k_caps_results_and_order_deterministic():
    from src.search.unified import unified_search

    with (
        patch("src.search.unified._chunk_search", _fake_chunks),
        patch("src.search.unified._knowledge_search", _fake_knowledge),
        patch("src.search.unified._glossary_search", _fake_glossary),
    ):
        a = unified_search(
            "invoices orders revenue",
            corpus_ids=["c1"],
            user_groups=["g"],
            granted_domains=["d"],
            tables=TABLES,
            metrics=METRICS,
            k=3,
        )
        b = unified_search(
            "invoices orders revenue",
            corpus_ids=["c1"],
            user_groups=["g"],
            granted_domains=["d"],
            tables=TABLES,
            metrics=METRICS,
            k=3,
        )
    assert len(a) == 3
    assert a == b


def test_table_scoring_prefers_term_overlap():
    from src.search.unified import _table_scores

    scored = _table_scores("customer orders revenue", TABLES)
    assert scored[0]["table_id"] == "t_orders"
    assert scored[0]["score"] > 0


def test_metric_scoring_prefers_term_overlap():
    from src.search.unified import _metric_scores

    scored = _metric_scores("monthly recurring revenue", METRICS)
    assert scored[0]["id"] == "finance/mrr"
    assert scored[0]["score"] > 0
    # matches on a synonym too
    syn = _metric_scores("wau", METRICS)
    assert syn and syn[0]["id"] == "product/wau"


def test_none_grants_mean_unfiltered_privileged_viewer():
    """None (admin) must NOT be treated as fail-closed — repo gets None filters."""
    from src.search.unified import unified_search

    captured = {}

    def spy_knowledge(query, **kw):
        captured.update(kw)
        return [{"id": "ki1", "title": "T", "content": "C", "domain": "d"}]

    with (
        patch("src.search.unified._chunk_search", _fake_chunks),
        patch("src.search.unified._knowledge_search", spy_knowledge),
        patch("src.search.unified._glossary_search", _no_glossary),
    ):
        hits = unified_search(
            "invoices", corpus_ids=["c1"], user_groups=None, granted_domains=None, tables=[], metrics=[], k=5
        )
    assert any(h["type"] == "knowledge" for h in hits)
    assert captured["user_groups"] is None
    assert captured["granted_domains"] is None
