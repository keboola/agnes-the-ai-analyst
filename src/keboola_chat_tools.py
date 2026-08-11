"""Derive a Keboola MCP stdio source from a registered ``source_connection``.

An admin who has already registered a Keboola project as a source connection
(stack URL + storage token in the vault) should not have to register the same
project a second time under ``/admin/mcp`` just to give the chat agent
Keboola's own tools. This module owns the one-way derivation:

    source_connections row  ->  mcp_sources row (transport='stdio')

The upstream server is Keboola's own ``keboola-mcp-server`` package, run as a
subprocess by ``connectors/mcp/client.py::_open_session``. The token is
injected through ``auth_secret_env`` (vault -> subprocess env), never on argv
— see ``references/security.md`` ("never put secrets on argv").

Two details of the launch command are load-bearing and were both established
by running the thing (2026-08-10):

* ``--prerelease=allow`` — current ``keboola-mcp-server`` releases depend on
  ``toon-format>=0.9.0b1``, a pre-release. uv refuses pre-releases by default
  and *silently resolves backwards* to the newest release whose dependencies
  are all stable: 1.32.0, roughly forty releases old, exposing 33 tools
  instead of 37 and missing the semantic-layer tools entirely. The failure
  mode is a working server with a quietly truncated toolset, so this flag is
  a correctness fix, not a convenience.
* the pinned version — same reason from the other direction: an unpinned
  ``--from keboola-mcp-server`` makes the toolset a function of whatever uv
  resolved on the day the source was created.
"""

from __future__ import annotations

from typing import Any, Dict, List

# Upstream package. Bump deliberately — the tool surface the agent sees is a
# function of this pin, so a bump is a user-visible change and wants a
# CHANGELOG bullet.
KEBOOLA_MCP_PACKAGE = "keboola-mcp-server"
KEBOOLA_MCP_VERSION = "1.74.6"

# The env var name the upstream server reads its token from. Also the name
# ``mcp_sources.auth_secret_env`` carries, so the vault value lands under the
# name the subprocess expects.
TOKEN_ENV = "KBC_STORAGE_TOKEN"
STACK_URL_ENV = "KBC_STORAGE_API_URL"

# uv ships in the runtime image at /usr/local/bin/uv (Dockerfile), so a bare
# name resolves through the PATH the MCP SDK inherits (its
# DEFAULT_INHERITED_ENV_VARS includes PATH and HOME).
RUNNER_COMMAND = "uv"


def derived_source_id(connection_id: str) -> str:
    """Stable ``mcp_sources.id`` for a connection's derived source.

    Deterministic rather than a fresh uuid so enabling twice re-syncs one row
    instead of accumulating duplicates, and so disable can find the row from
    the connection id alone.
    """
    return f"kbc-chat-{connection_id}"


def derived_source_name(connection_name: str) -> str:
    """Human-facing ``mcp_sources.name``. Unique-constrained upstream, so it
    carries the connection name — which is itself unique among connections."""
    return f"Keboola: {connection_name}"


def runner_args(*, version: str = KEBOOLA_MCP_VERSION) -> List[str]:
    """Argv for ``uv`` that runs a pinned ``keboola-mcp-server`` over stdio."""
    return [
        "tool",
        "run",
        "--prerelease=allow",
        "--from",
        f"{KEBOOLA_MCP_PACKAGE}=={version}",
        "keboola_mcp_server",
        "--transport",
        "stdio",
    ]


def build_stdio_spec(
    *,
    connection_id: str,
    connection_name: str,
    stack_url: str,
    version: str = KEBOOLA_MCP_VERSION,
) -> Dict[str, Any]:
    """Build the ``mcp_sources`` row for a Keboola connection.

    ``scope='shared'``: the connection's token is one project-wide credential
    held by the admin, not a per-analyst one. Which analysts may call the
    resulting tools is decided downstream by ``tool_grants`` — the derived
    source lands with no grants at all, so enabling it exposes nothing until
    an admin grants explicitly.
    """
    return {
        "id": derived_source_id(connection_id),
        "name": derived_source_name(connection_name),
        "transport": "stdio",
        "command": RUNNER_COMMAND,
        "args": runner_args(version=version),
        "env": {STACK_URL_ENV: stack_url.rstrip("/")},
        "auth_secret_env": TOKEN_ENV,
        "auth_method": None,
        "scope": "shared",
        "enabled": True,
        "connect_hint": (
            "Derived from the Keboola source connection of the same name. "
            "Rotating that connection's storage token propagates here on its own."
        ),
    }
