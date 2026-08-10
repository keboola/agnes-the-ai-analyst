"""The `sources` block, and the claim it makes being checkable.

The point of the block is not that an answer *says* where a number came from —
a prose sentence did that, unreliably. It is that the saying is parseable, so
absence is visible and a wrong claim can be contradicted by the turn's own tool
calls.

These tests are therefore mostly about the unflattering paths: no block, a
block naming a table nothing queried, a block with only assumptions. The happy
path is one case; the ways it can be wrong are the rest.
"""

from __future__ import annotations

import pytest

from app.chat.sources import (
    SourceClaim,
    extract_block,
    parse_claims,
    verdict,
)

TOOL_CALLS = [
    {"tool": "Bash", "args": {"command": 'agnes query --sql "SELECT month, mrr FROM mrr ORDER BY 1"'}},
    {"tool": "Bash", "args": {"command": "agnes catalog --metrics --show sales_revenue/mrr"}},
]


def _answer(block: str) -> str:
    return f"MRR is $1,126,632.\n\n```sources\n{block}\n```\n"


# ── parsing ─────────────────────────────────────────────────────────────────


def test_block_is_found_and_claims_keep_their_order():
    body = extract_block(_answer("table: mrr\nmetric: sales_revenue/mrr"))
    assert body is not None
    claims = parse_claims(body)
    assert [(c.kind, c.ref) for c in claims] == [("table", "mrr"), ("metric", "sales_revenue/mrr")]


def test_backticks_and_stray_lines_do_not_cost_the_reader_the_block():
    """A model reaching for markdown habits inside the block, plus a blank line
    and a line that is not a claim at all. None of it is an error worth
    discarding real provenance over."""
    claims = parse_claims("table: `mrr`\n\nsomething the model wrote\nmetric: sales_revenue/mrr\n")
    assert [(c.kind, c.ref) for c in claims] == [("table", "mrr"), ("metric", "sales_revenue/mrr")]


def test_a_repeated_claim_is_shown_once():
    claims = parse_claims("table: mrr\ntable: mrr\n")
    assert len(claims) == 1


def test_no_block_is_not_an_empty_block():
    """The distinction the whole feature rests on: an answer that declared
    nothing must not look like one that declared and had nothing to say."""
    v = verdict("MRR is $1,126,632.", TOOL_CALLS)
    assert v.declared is False
    assert v.claims == []


# ── verification ────────────────────────────────────────────────────────────


def test_a_claim_the_tool_calls_support_is_verified():
    v = verdict(_answer("table: mrr\nmetric: sales_revenue/mrr"), TOOL_CALLS)
    assert v.declared is True
    assert all(c.verified for c in v.claims)
    assert v.unverified == []


def test_a_table_nothing_queried_is_reported_unverified():
    """The case that separates this from a prompt rule. The answer claims a
    table; the record of what ran does not contain it; the reader is told."""
    v = verdict(_answer("table: mrr\ntable: hr_headcount"), TOOL_CALLS)
    assert [c.ref for c in v.unverified] == ["hr_headcount"]


def test_an_assumption_is_never_marked_unverified():
    """There is nothing to check a stated assumption against. Marking honest
    assumptions as unverified would train the reader to ignore the badge."""
    v = verdict(_answer("table: mrr\nassumption: excludes contractors"), TOOL_CALLS)
    by_kind = {c.kind: c for c in v.claims}
    assert by_kind["assumption"].verified is None
    assert by_kind["table"].verified is True


def test_a_metric_cited_by_bare_name_still_verifies():
    """`sales_revenue/mrr` and `mrr` are the same metric; a correct citation
    must not read as unverified over punctuation."""
    v = verdict(_answer("metric: sales_revenue/mrr"), [{"args": {"command": "agnes catalog --metrics --show mrr"}}])
    assert v.claims[0].verified is True


def test_no_tool_calls_means_nothing_is_verified():
    """An answer that ran nothing cannot have a supported claim — and this is
    the shape a fabricated citation takes."""
    v = verdict(_answer("table: mrr"), None)
    assert v.claims[0].verified is False


@pytest.mark.parametrize(
    "calls",
    [
        ["agnes query --sql 'SELECT * FROM mrr'"],  # plain strings
        [{"tool": "x", "args": {"nested": {"sql": "FROM mrr"}}}],  # nested dicts
        [{"unserializable": object()}, {"args": {"command": "FROM mrr"}}],  # one bad entry
    ],
    ids=["strings", "nested", "unserializable-entry"],
)
def test_tool_call_shapes_do_not_break_verification(calls):
    """tool_calls is untyped JSON off the wire. A shape the serializer chokes
    on must degrade to 'unverified', never to a 500 on the messages endpoint."""
    v = verdict(_answer("table: mrr"), calls)
    assert v.claims[0].verified is True


def test_verdict_serializes_for_the_wire():
    v = verdict(_answer("table: mrr\nassumption: x"), TOOL_CALLS)
    assert v.to_dict() == {
        "declared": True,
        "claims": [
            {"kind": "table", "ref": "mrr", "verified": True},
            {"kind": "assumption", "ref": "x", "verified": None},
        ],
    }


def test_claim_is_immutable():
    """Verdicts are recomputed per read; a mutable claim would invite a caller
    to 'fix' one in place and diverge from what the tool calls say."""
    with pytest.raises(Exception):
        SourceClaim(kind="table", ref="mrr").ref = "other"  # type: ignore[misc]
