"""Unit tests for per-session BQ scan budget (Task 12.3).

These tests exercise the in-memory ``accumulate_session_bq_bytes`` helper
directly — no HTTP layer needed. The integration with the JWT / request.state
path is deferred to Task 13.1.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.api.query as query_module
from app.api.query import accumulate_session_bq_bytes, _per_session_bq_bytes


@pytest.fixture(autouse=True)
def _reset_session_bytes():
    """Clear the in-memory counter before and after each test."""
    _per_session_bq_bytes.clear()
    yield
    _per_session_bq_bytes.clear()


def test_accumulate_within_budget_does_not_raise():
    """Scans below the limit accumulate without raising."""
    limit = 10 * 1024**3  # 10 GiB
    accumulate_session_bq_bytes("sess1", 1 * 1024**3, limit_bytes=limit)
    accumulate_session_bq_bytes("sess1", 2 * 1024**3, limit_bytes=limit)
    assert _per_session_bq_bytes["sess1"] == 3 * 1024**3


def test_accumulate_exceeds_budget_raises_bq_budget_exhausted():
    """Once cumulative bytes exceed the limit, HTTPException 400 bq_budget_exhausted."""
    limit = 5 * 1024**3  # 5 GiB
    accumulate_session_bq_bytes("sess2", 3 * 1024**3, limit_bytes=limit)
    with pytest.raises(HTTPException) as exc_info:
        accumulate_session_bq_bytes("sess2", 3 * 1024**3, limit_bytes=limit)
    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert detail["reason"] == "bq_budget_exhausted"
    assert detail["session_id"] == "sess2"
    assert detail["scan_bytes_cumulative"] == 6 * 1024**3
    assert detail["limit_bytes"] == limit


def test_accumulate_exact_limit_does_not_raise():
    """Exactly hitting the limit is allowed (rejection is strictly over)."""
    limit = 4 * 1024**3
    accumulate_session_bq_bytes("sess3", 4 * 1024**3, limit_bytes=limit)
    # No exception raised — equal is still allowed


def test_separate_sessions_tracked_independently():
    """Different session IDs have independent byte counters."""
    limit = 5 * 1024**3
    accumulate_session_bq_bytes("sessA", 4 * 1024**3, limit_bytes=limit)
    # sessB starts fresh — should not inherit sessA's bytes
    accumulate_session_bq_bytes("sessB", 4 * 1024**3, limit_bytes=limit)
    assert _per_session_bq_bytes["sessA"] == 4 * 1024**3
    assert _per_session_bq_bytes["sessB"] == 4 * 1024**3


def test_zero_limit_disables_cap():
    """Setting limit_bytes=0 disables the cap (no exception on any scan size)."""
    accumulate_session_bq_bytes("sess4", 100 * 1024**3, limit_bytes=0)
    # No exception raised
