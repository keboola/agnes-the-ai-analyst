"""Contract: every Caddy reverse_proxy that fronts the app must flush
immediately, or Server-Sent Events (the cloud-chat LLM proxy
`/api/broker/anthropic` and the MCP endpoint `/api/mcp`) get buffered into
one end-of-turn burst instead of streaming.

Why a guard and not just the directive: Caddy only auto-flushes when the
response Content-Type is EXACTLY `text/event-stream`, but Starlette appends
`; charset=utf-8`, defeating auto-detection. `flush_interval -1` forces the
flush regardless. This was diagnosed live (mac→Caddy→app→Anthropic delivered
every token delta at a single timestamp; the same request straight to the
app streamed). These text assertions keep a future Caddyfile edit from
silently dropping the directive and re-breaking chat streaming — no Caddy
binary needed.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _app_proxy_blocks(text: str) -> list[str]:
    """Return the body of every `reverse_proxy ...app/api... { ... }` block."""
    blocks = []
    i = 0
    while True:
        j = text.find("reverse_proxy", i)
        if j == -1:
            break
        brace = text.find("{", j)
        if brace == -1:
            break
        depth = 1
        k = brace + 1
        while k < len(text) and depth:
            if text[k] == "{":
                depth += 1
            elif text[k] == "}":
                depth -= 1
            k += 1
        blocks.append(text[j:k])
        i = k
    return blocks


def test_main_caddyfile_app_proxy_flushes_immediately():
    text = (_ROOT / "Caddyfile").read_text()
    app_blocks = [b for b in _app_proxy_blocks(text) if "app:8000" in b]
    assert app_blocks, "expected reverse_proxy app:8000 blocks in Caddyfile"
    for b in app_blocks:
        assert "flush_interval -1" in b, (
            "a reverse_proxy app:8000 block is missing `flush_interval -1` — "
            "SSE (chat LLM proxy / MCP) will be buffered by Caddy"
        )


def test_mtier_api_proxy_flushes_immediately():
    text = (_ROOT / "deploy" / "caddy" / "Caddyfile.mtier").read_text()
    api_blocks = [b for b in _app_proxy_blocks(text) if "api1:8000" in b or "api2:8000" in b]
    assert api_blocks, "expected the api replica reverse_proxy block in Caddyfile.mtier"
    for b in api_blocks:
        assert "flush_interval -1" in b, "mtier api reverse_proxy must flush SSE immediately"


def test_apps_subdomain_proxy_flushes_immediately():
    text = (_ROOT / "deploy" / "caddy" / "Caddyfile.apps-subdomain").read_text()
    blocks = [b for b in _app_proxy_blocks(text) if "app:8000" in b]
    assert blocks and all("flush_interval -1" in b for b in blocks)
