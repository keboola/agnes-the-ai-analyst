"""Resolve table_registry rows to a named connection + credentials.

Resolution (spec 2026-06-12 §3.2):
  connection_id -> that connection; NULL -> default for source_type;
  nothing registered -> None (caller falls back to the legacy env path
  during the deprecation window).
Token chain: vault -> token_env -> None.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def resolve_connection(source_type: str, connection_id: str | None) -> dict[str, Any] | None:
    from src.repositories import source_connections_repo

    repo = source_connections_repo()
    if connection_id:
        return repo.get(connection_id)
    return repo.get_default(source_type)


def resolve_token(connection: dict[str, Any]) -> str | None:
    from src.repositories import connection_secrets_repo

    secret = connection_secrets_repo().get(connection["id"])
    if secret:
        return secret
    token_env = connection.get("token_env")
    if token_env:
        return os.environ.get(token_env) or None
    return None


def resolve_connection_by_alias(alias: str) -> dict[str, Any] | None:
    """Resolve a Keboola-style DuckDB ATTACH alias to its source connection."""
    from src.repositories import source_connections_repo

    return source_connections_repo().get_by_alias(alias)


def resolve_token_by_alias(alias: str) -> str | None:
    """Resolve the credential for a connection by its DuckDB ATTACH alias.

    Used by the orchestrator / query-path re-attach logic, which has the
    alias from ``_remote_attach`` but not the connection_id.
    """
    conn = resolve_connection_by_alias(alias)
    if not conn:
        return None
    return resolve_token(conn)
