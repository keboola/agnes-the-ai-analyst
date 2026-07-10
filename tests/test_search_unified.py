"""unified_search — fan-out over chunks + knowledge + catalog cards (K2)."""

from __future__ import annotations

from unittest.mock import patch

TABLES = [
    {"id": "t_orders", "name": "orders", "description": "customer orders and revenue", "columns_json": None},
    {"id": "t_web", "name": "web_sessions", "description": "web analytics sessions", "columns_json": None},
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


def test_merges_all_three_sources():
    from src.search.unified import unified_search

    with (
        patch("src.search.unified._chunk_search", _fake_chunks),
        patch("src.search.unified._knowledge_search", _fake_knowledge),
    ):
        hits = unified_search(
            "invoices orders",
            corpus_ids=["c1"],
            user_groups=["g1"],
            granted_domains=["d1"],
            tables=TABLES,
            k=10,
        )
    types = {h["type"] for h in hits}
    assert types == {"chunk", "knowledge", "table"}
    table_hit = next(h for h in hits if h["type"] == "table")
    assert table_hit["table_id"] == "t_orders"
    assert "agnes query" in table_hit["pivot_hint"]


def test_fail_closed_per_source():
    from src.search.unified import unified_search

    with (
        patch("src.search.unified._chunk_search", _fake_chunks),
        patch("src.search.unified._knowledge_search", _fake_knowledge),
    ):
        hits = unified_search("invoices", corpus_ids=[], user_groups=[], granted_domains=[], tables=[], k=10)
    assert hits == []


def test_blank_query_returns_empty():
    from src.search.unified import unified_search

    assert unified_search("  ", corpus_ids=["c1"], user_groups=["g"], granted_domains=["d"], tables=TABLES) == []


def test_k_caps_results_and_order_deterministic():
    from src.search.unified import unified_search

    with (
        patch("src.search.unified._chunk_search", _fake_chunks),
        patch("src.search.unified._knowledge_search", _fake_knowledge),
    ):
        a = unified_search(
            "invoices orders",
            corpus_ids=["c1"],
            user_groups=["g"],
            granted_domains=["d"],
            tables=TABLES,
            k=2,
        )
        b = unified_search(
            "invoices orders",
            corpus_ids=["c1"],
            user_groups=["g"],
            granted_domains=["d"],
            tables=TABLES,
            k=2,
        )
    assert len(a) == 2
    assert a == b


def test_table_scoring_prefers_term_overlap():
    from src.search.unified import _table_scores

    scored = _table_scores("customer orders revenue", TABLES)
    assert scored[0]["table_id"] == "t_orders"
    assert scored[0]["score"] > 0


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
    ):
        hits = unified_search(
            "invoices", corpus_ids=["c1"], user_groups=None, granted_domains=None, tables=[], k=5
        )
    assert any(h["type"] == "knowledge" for h in hits)
    assert captured["user_groups"] is None
    assert captured["granted_domains"] is None
