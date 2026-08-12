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

import hashlib
import os
import re
from pathlib import Path
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
# Only a MASTER token gets a workspace created for it behind the scenes. A
# custom token — the one an admin reaches for when they want the agent to have
# read-only rights — has to be told which workspace to run SQL in, or
# `query_data` fails while every other tool works. Optional here because the
# master-token setup genuinely does not need it.
WORKSPACE_SCHEMA_ENV = "KBC_WORKSPACE_SCHEMA"

# uv ships in the runtime image at /usr/local/bin/uv (Dockerfile), so a bare
# name resolves through the PATH the MCP SDK inherits (its
# DEFAULT_INHERITED_ENV_VARS includes PATH and HOME).
RUNNER_COMMAND = "uv"


def uv_cache_dir() -> str:
    """Where ``uv`` caches the downloaded package, pinned onto the data disk.

    Two reasons not to leave this to uv's default (``$HOME/.cache/uv``):

    * The runtime image never sets ``HOME`` (``python:3.13-slim`` doesn't, and
      a ``USER`` directive alone doesn't either), and the MCP SDK forwards only
      env vars that actually exist in the parent — so the subprocess can end up
      with no ``HOME`` to derive a cache path from.
    * The container's filesystem is thrown away on every upgrade. A cache
      inside it means the first tool call after each auto-upgrade re-downloads
      the package; on the data volume it survives.
    """
    return str(Path(os.environ.get("DATA_DIR", "./data")) / "cache" / "uv")


def derived_source_id(connection_id: str) -> str:
    """Stable ``mcp_sources.id`` for a connection's derived source.

    Deterministic rather than a fresh uuid so enabling twice re-syncs one row
    instead of accumulating duplicates, and so disable can find the row from
    the connection id alone.
    """
    return f"kbc-chat-{connection_id}"


def tool_name_prefix(connection_id: str, connection_name: str) -> str:
    """Prefix that keeps two connected projects' identically-named tools apart.

    Every Keboola project exposes ``query_data``, ``get_buckets`` and so on, so
    the exposed names must carry which project they reach — the agent picks a
    tool by name and has nothing else to go on. The readable half comes from
    the connection name; the four hex characters come from the connection id,
    because two distinct names can sanitize to the same slug and a collision
    would silently point one project's tool at another's data.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", (connection_name or "").lower()).strip("_")[:24]
    digest = hashlib.sha256(connection_id.encode("utf-8")).hexdigest()[:4]
    return f"kbc_{slug}_{digest}" if slug else f"kbc_{digest}"


def derived_tool_id(connection_id: str, tool_name: str) -> str:
    """Stable ``tool_registry.tool_id``, so re-enabling updates rows in place."""
    return f"{derived_source_id(connection_id)}__{tool_name}"


# Model APIs cap tool names (^[a-zA-Z0-9_-]{1,64}$); an over-long name fails at
# the model call and can poison the whole tool list, not just the one tool.
TOOL_NAME_MAX = 64


def exposed_tool_name(connection_id: str, connection_name: str, tool_name: str) -> str:
    """``{prefix}_{tool_name}``, capped at ``TOOL_NAME_MAX``.

    Under the cap this is exactly the prefixed name (so existing registrations
    are unaffected). Over it, the connection-name slug — the readable, least
    load-bearing part — shrinks first; the 4-hex connection digest never does,
    because it is what keeps two projects' identically-named tools apart. If
    even a slug-less name is too long, the upstream name keeps its head and
    gains a 4-hex digest of its full self, so two long names cannot collide.
    """
    full = f"{tool_name_prefix(connection_id, connection_name)}_{tool_name}"
    if len(full) <= TOOL_NAME_MAX:
        return full
    digest = hashlib.sha256(connection_id.encode("utf-8")).hexdigest()[:4]
    slug = re.sub(r"[^a-z0-9]+", "_", (connection_name or "").lower()).strip("_")[:24]
    keep = len(slug) - (len(full) - TOOL_NAME_MAX)
    slug = slug[:keep].rstrip("_") if keep > 0 else ""
    candidate = f"kbc_{slug}_{digest}_{tool_name}" if slug else f"kbc_{digest}_{tool_name}"
    if len(candidate) <= TOOL_NAME_MAX:
        return candidate
    name_digest = hashlib.sha256(tool_name.encode("utf-8")).hexdigest()[:4]
    head = TOOL_NAME_MAX - len(f"kbc_{digest}__{name_digest}")
    return f"kbc_{digest}_{tool_name[:head]}_{name_digest}"


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
    workspace_schema: str | None = None,
    version: str = KEBOOLA_MCP_VERSION,
) -> Dict[str, Any]:
    """Build the ``mcp_sources`` row for a Keboola connection.

    ``scope='shared'``: the connection's token is one project-wide credential
    held by the admin, not a per-analyst one. Which analysts may call the
    resulting tools is decided downstream by ``tool_grants`` — the derived
    source lands with no grants at all, so enabling it exposes nothing until
    an admin grants explicitly.

    ``workspace_schema`` is passed through when the connection carries one
    (``config.workspace_schema``). It is what makes a non-master token usable:
    with a master token Keboola creates the workspace itself, so the setting
    stays absent rather than being invented.
    """
    return {
        "id": derived_source_id(connection_id),
        "name": derived_source_name(connection_name),
        "transport": "stdio",
        "command": RUNNER_COMMAND,
        "args": runner_args(version=version),
        "env": {
            STACK_URL_ENV: stack_url.rstrip("/"),
            "UV_CACHE_DIR": uv_cache_dir(),
            **({WORKSPACE_SCHEMA_ENV: workspace_schema} if workspace_schema else {}),
        },
        "auth_secret_env": TOKEN_ENV,
        "auth_method": None,
        "scope": "shared",
        "enabled": True,
        "connect_hint": (
            "Derived from the Keboola source connection of the same name. "
            "Rotating that connection's storage token propagates here on its own."
        ),
    }
