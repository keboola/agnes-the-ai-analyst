"""Universal MCP — outbound tool generation and passthrough forwarding.

Sits alongside @mf's `app/api/mcp_http.py` (HTTP/SSE MCP server) and
`cli/mcp/server.py` (stdio MCP server) — those provide the generic tools
(catalog/schema/query); this module adds tools dynamically registered from
``tool_registry`` (Universal MCP — RFC #461 §7).
"""
