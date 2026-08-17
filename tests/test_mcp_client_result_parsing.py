"""What counts as "the tool returned data" — and what the failure must say.

``_to_call_result`` reduced an MCP tool call to text + JSON by concatenating
every ``TextContent`` block and running ``json.loads`` over the join. Two
upstream shapes that are correct per the MCP spec died there:

* **structured content.** A server whose tool declares an ``outputSchema``
  answers with ``structuredContent`` (parsed JSON) and is free to put a
  human-readable rendering in ``content``. The call site passed only
  ``result.content``, so the structured half was discarded and the prose half
  failed to parse — the tool looked like it returned nothing.
* **one JSON document per block.** Two valid JSON objects joined by ``\\n``
  are not a valid JSON document, so a per-block emitter parsed as nothing.

Materialize mode reports that as ``tool X did not return parseable JSON``,
which names the requirement but not what arrived — so the only way to learn
the actual shape was to reproduce the call by hand. These tests pin the two
parses and the diagnostics on the failure.
"""

from __future__ import annotations

import json

import pytest

from connectors.mcp.client import ToolCallResult, _to_call_result


class _Block:
    """Minimal stand-in for an MCP content block (SDK models expose .type/.text)."""

    def __init__(self, text: str | None, type_: str = "text"):
        self.text = text
        self.type = type_


def test_structured_content_is_preferred_over_prose():
    structured = {"data_apps": [{"id": "1", "name": "Sales", "url": "https://example.com/a"}]}
    result = _to_call_result(
        [_Block("Here are the data apps in your project:\n\n- Sales")],
        structured=structured,
    )
    assert result.data == structured
    assert result.structured_present is True
    # The prose is still available — surfaces that render a tool call show it.
    assert "Sales" in result.text


def test_structured_content_wins_even_when_the_text_also_parses():
    """Both halves parseable: the structured one is authoritative.

    A server may render a *summary* into the text block while the structured
    half carries the full rows; taking the text would silently truncate.
    """
    structured = {"rows": [{"id": "1"}, {"id": "2"}]}
    result = _to_call_result([_Block(json.dumps({"rows": [{"id": "1"}]}))], structured=structured)
    assert result.data == structured


def test_json_in_a_single_text_block_still_parses():
    payload = {"accounts": [{"id": "a-1"}], "total": 1}
    result = _to_call_result([_Block(json.dumps(payload))])
    assert result.data == payload
    assert result.structured_present is False


def test_one_json_document_per_block_becomes_a_list():
    """Blocks that each parse become the rows — that IS the table shape."""
    blocks = [_Block(json.dumps({"id": "1"})), _Block(json.dumps({"id": "2"}))]
    result = _to_call_result(blocks)
    assert result.data == [{"id": "1"}, {"id": "2"}]


def test_partially_parseable_blocks_do_not_invent_data():
    """One JSON block next to prose is not a table — better nothing than a guess."""
    blocks = [_Block(json.dumps({"id": "1"})), _Block("...and 4 more, see the UI")]
    result = _to_call_result(blocks)
    assert result.data is None


def test_prose_only_reports_what_arrived():
    blocks = [_Block("# Data apps\n\n1. Sales dashboard"), _Block(None, type_="image")]
    result = _to_call_result(blocks)
    assert result.data is None
    assert result.structured_present is False
    # The diagnostics the materialize error needs: how many blocks, of what type.
    assert result.block_types == ("text", "image")


def test_defaults_keep_existing_construction_working():
    """Callers (and tests) that build the result positionally must not break."""
    result = ToolCallResult(text="{}", data={}, is_error=False)
    assert result.structured_present is False
    assert result.block_types == ()


def test_materialize_failure_names_what_arrived(monkeypatch, tmp_path):
    """The error a failing materialize leaves behind must be actionable.

    Without this, `materialize_failed` says only what the mode requires; the
    operator's next step is to reproduce the upstream call by hand — with
    credentials that live in the vault write-only, so often they cannot.
    """
    import asyncio

    import connectors.mcp.client as mcp_client
    from connectors.mcp import extractor as mcp_extractor

    async def _call(source, tool_name, arguments=None, *, caller_user_id=None):
        return _to_call_result([_Block("# Data apps\n\n1. Sales dashboard")])

    monkeypatch.setattr(mcp_client, "call_tool_async", _call)

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(
            mcp_extractor._materialize_one_tool_async(
                source={"id": "src_x", "name": "Keboola"},
                tool={"original_name": "get_data_apps", "exposed_name": "kbc_data_apps"},
                output_path=tmp_path,
            )
        )

    message = str(excinfo.value)
    assert "get_data_apps" in message
    # What arrived: no structured half, one text block, and a sample of it.
    assert "structuredContent" in message
    assert "text" in message
    assert "Data apps" in message
