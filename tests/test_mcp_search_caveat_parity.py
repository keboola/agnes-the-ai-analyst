"""Both MCP servers must warn about the same search behaviour.

`tests/test_mcp_tool_parity.py` guards that the transports expose the same
tool NAMES. Nothing guarded what those tools SAY — and the stdio server
(`cli/mcp/server.py`) hand-maintains its own docstrings rather than
importing the HTTP ones, so guidance added to
`app/api/mcp/foundation_tools.py` does not reach it.

That split is exactly backwards for this fix: `app/chat/runner.py` spawns
`agnes mcp` — the stdio server — for the in-chat agent, so the surface
where the "I don't have access to your files or collections" answer was
actually produced was the one still missing the warning. Devin Review
caught it on this PR.

A docstring is what the model reads before its first call, so this is the
preventive half of the change; the runtime `hint` is the fallback.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTTP_TOOLS = ROOT / "app" / "api" / "mcp" / "foundation_tools.py"
STDIO_TOOLS = ROOT / "cli" / "mcp" / "server.py"

#: The three retrieval behaviours that make a reasonable query miss. Each
#: entry is (label, regex) — matched case-insensitively against a tool's
#: docstring in both servers.
#: Patterns tolerate line wrapping (``\s+``) — a docstring reflows, and a
#: guard that fails on where the line broke would be noise, not signal.
CAVEATS = (
    ("filenames are not indexed", r"filename[s]?\s+are\s+not\s+indexed"),
    ("whole-word matching", r"whole[\s-]+word"),
    ("no wildcard", r"wildcard"),
)

SEARCH_TOOLS = ("collections_search", "knowledge_search")


def _docstring(source: str, func: str) -> str:
    """The triple-quoted docstring of ``def func`` / ``async def func``."""
    m = re.search(
        rf"(?:async\s+)?def\s+{re.escape(func)}\s*\(.*?\)\s*->[^:]*:\s*\"\"\"(.*?)\"\"\"",
        source,
        re.S,
    )
    assert m, f"could not find {func}'s docstring"
    return m.group(1)


@pytest.mark.parametrize("tool", SEARCH_TOOLS)
@pytest.mark.parametrize(("label", "pattern"), CAVEATS, ids=[c[0] for c in CAVEATS])
def test_http_tool_documents_the_caveat(tool, label, pattern):
    doc = _docstring(HTTP_TOOLS.read_text(encoding="utf-8"), tool)
    assert re.search(pattern, doc, re.I), f"{tool} (HTTP) does not mention: {label}"


@pytest.mark.parametrize("tool", SEARCH_TOOLS)
@pytest.mark.parametrize(("label", "pattern"), CAVEATS, ids=[c[0] for c in CAVEATS])
def test_stdio_tool_documents_the_caveat(tool, label, pattern):
    """The stdio server backs the in-chat agent — the incident surface."""
    doc = _docstring(STDIO_TOOLS.read_text(encoding="utf-8"), tool)
    assert re.search(pattern, doc, re.I), f"{tool} (stdio) does not mention: {label}"


@pytest.mark.parametrize("tool", SEARCH_TOOLS)
def test_both_servers_say_an_empty_result_is_not_an_access_problem(tool):
    """The wrong inference the whole change set exists to prevent."""
    for source, which in ((HTTP_TOOLS, "HTTP"), (STDIO_TOOLS, "stdio")):
        doc = _docstring(source.read_text(encoding="utf-8"), tool).lower()
        assert "access" in doc, f"{tool} ({which}) never rules out the access reading"


def test_the_guard_reads_real_docstrings():
    """A parser that silently matches nothing would pass every test above."""
    for source in (HTTP_TOOLS, STDIO_TOOLS):
        for tool in SEARCH_TOOLS:
            assert len(_docstring(source.read_text(encoding="utf-8"), tool)) > 200
