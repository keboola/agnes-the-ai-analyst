"""Unit tests for app/api/mcp_policy.py — the three RFC #461 §3 gates.

Pure tests with no FastAPI / DB / mcp dependencies.
"""
from __future__ import annotations

import pytest

from app.api.mcp_policy import (
    REDACTED_TOKEN,
    MutatingNotAllowed,
    RateLimited,
    check_mutating,
    check_rate_limit,
    redact_pii,
    redact_response,
    reset_rate_buckets_for_tests,
)


# ── mutating gate ─────────────────────────────────────────────────────────


def test_mutating_gate_admin_passes_through():
    check_mutating({"tool_id": "t.create", "mutating": True}, is_admin=True)  # no raise


def test_mutating_gate_non_admin_blocked_on_mutating_tool():
    with pytest.raises(MutatingNotAllowed):
        check_mutating({"tool_id": "t.create", "mutating": True}, is_admin=False)


def test_mutating_gate_non_admin_allowed_on_read_only_tool():
    check_mutating({"tool_id": "t.read", "mutating": False}, is_admin=False)
    check_mutating({"tool_id": "t.read"}, is_admin=False)  # missing key defaults False


# ── PII redaction ─────────────────────────────────────────────────────────


def test_redact_top_level_keys():
    out = redact_pii({"name": "Alice", "email": "a@x"}, ["email"])
    assert out == {"name": "Alice", "email": REDACTED_TOKEN}


def test_redact_recurses_into_nested_dict():
    inp = {"user": {"name": "Alice", "email": "a@x"}, "id": 1}
    out = redact_pii(inp, ["email"])
    assert out == {"user": {"name": "Alice", "email": REDACTED_TOKEN}, "id": 1}


def test_redact_recurses_into_list_of_dicts():
    inp = {"contacts": [{"email": "a@x", "name": "A"}, {"email": "b@y", "name": "B"}]}
    out = redact_pii(inp, ["email"])
    assert out == {
        "contacts": [
            {"email": REDACTED_TOKEN, "name": "A"},
            {"email": REDACTED_TOKEN, "name": "B"},
        ]
    }


def test_redact_empty_keys_passes_through():
    inp = {"email": "a@x"}
    assert redact_pii(inp, []) is inp


def test_redact_response_keeps_text_and_data_in_sync():
    text, data = redact_response(
        text='{"email": "a@x", "name": "Alice"}',
        data={"email": "a@x", "name": "Alice"},
        pii_fields=["email"],
    )
    assert data == {"email": REDACTED_TOKEN, "name": "Alice"}
    # text is the JSON serialization of the redacted data
    assert "[REDACTED]" in text
    assert "a@x" not in text


def test_redact_response_skips_when_data_is_none():
    text, data = redact_response(
        text="plain text from upstream",
        data=None,
        pii_fields=["email"],
    )
    assert text == "plain text from upstream"
    assert data is None


def test_redact_response_noop_without_pii_fields():
    text, data = redact_response(
        text='{"email": "a@x"}',
        data={"email": "a@x"},
        pii_fields=None,
    )
    assert text == '{"email": "a@x"}'
    assert data == {"email": "a@x"}


# ── rate limit ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_rate_buckets():
    reset_rate_buckets_for_tests()
    yield
    reset_rate_buckets_for_tests()


def test_rate_limit_disabled_when_cap_is_none_or_zero():
    # 100 calls, no cap → no raise
    for _ in range(100):
        check_rate_limit("t.a", "u1", None)
    for _ in range(100):
        check_rate_limit("t.a", "u1", 0)


def test_rate_limit_allows_calls_under_cap():
    # cap=3 → first three calls pass
    check_rate_limit("t.a", "u1", 3, now=1000.0)
    check_rate_limit("t.a", "u1", 3, now=1001.0)
    check_rate_limit("t.a", "u1", 3, now=1002.0)


def test_rate_limit_blocks_fourth_call_within_window():
    check_rate_limit("t.a", "u1", 3, now=1000.0)
    check_rate_limit("t.a", "u1", 3, now=1001.0)
    check_rate_limit("t.a", "u1", 3, now=1002.0)
    with pytest.raises(RateLimited) as ei:
        check_rate_limit("t.a", "u1", 3, now=1003.0)
    # The oldest timestamp is 1000.0; next free slot at 1060.0.
    # At now=1003.0 retry_after ≈ 57s.
    assert 56.0 <= ei.value.retry_after_seconds <= 58.0


def test_rate_limit_separate_keys_per_tool_and_user():
    # Saturate (tool=t.a, user=u1)
    for t in range(3):
        check_rate_limit("t.a", "u1", 3, now=1000.0 + t)
    # Other tool, same user — fresh quota
    check_rate_limit("t.b", "u1", 3, now=1003.0)
    # Same tool, other user — fresh quota
    check_rate_limit("t.a", "u2", 3, now=1003.0)


def test_rate_limit_old_calls_age_out_of_window():
    # Three calls at t=0,1,2 — bucket is full
    check_rate_limit("t.a", "u1", 3, now=1000.0)
    check_rate_limit("t.a", "u1", 3, now=1001.0)
    check_rate_limit("t.a", "u1", 3, now=1002.0)
    # 61s later the oldest aged out → new call accepted
    check_rate_limit("t.a", "u1", 3, now=1061.0)
