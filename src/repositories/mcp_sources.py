"""Repository for `mcp_sources` (v61) — external MCP servers Agnes ingests from."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import duckdb


class MCPSourceRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row, cols) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        d = dict(zip(cols, row))
        for k in ("args", "env"):
            if d.get(k) is not None and isinstance(d[k], str):
                try:
                    d[k] = json.loads(d[k])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    def upsert(
        self,
        *,
        id: str,
        name: str,
        transport: str,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        url: Optional[str] = None,
        auth_method: Optional[str] = None,
        auth_secret_env: Optional[str] = None,
        enabled: bool = True,
        scope: str = "shared",
    ) -> None:
        if transport not in ("stdio", "http", "sse"):
            raise ValueError(f"unsupported transport: {transport}")
        if transport == "stdio" and not command:
            raise ValueError("stdio transport requires 'command'")
        if transport in ("http", "sse") and not url:
            raise ValueError(f"{transport} transport requires 'url'")
        if scope not in ("shared", "per_user"):
            raise ValueError(f"unsupported scope: {scope!r}; must be 'shared' or 'per_user'")

        now = datetime.now(timezone.utc)
        args_json = json.dumps(args) if args is not None else None
        env_json = json.dumps(env) if env is not None else None
        self.conn.execute(
            """INSERT INTO mcp_sources
               (id, name, transport, command, args, env, url, auth_method, auth_secret_env, enabled, scope, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET
                   name = excluded.name,
                   transport = excluded.transport,
                   command = excluded.command,
                   args = excluded.args,
                   env = excluded.env,
                   url = excluded.url,
                   auth_method = excluded.auth_method,
                   auth_secret_env = excluded.auth_secret_env,
                   enabled = excluded.enabled,
                   scope = excluded.scope,
                   updated_at = excluded.updated_at""",
            [id, name, transport, command, args_json, env_json, url, auth_method, auth_secret_env, enabled, scope, now, now],
        )

    def get(self, source_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM mcp_sources WHERE id = ?", [source_id]).fetchone()
        cols = [d[0] for d in self.conn.description] if row else []
        return self._row_to_dict(row, cols)

    def get_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM mcp_sources WHERE name = ?", [name]).fetchone()
        cols = [d[0] for d in self.conn.description] if row else []
        return self._row_to_dict(row, cols)

    def list_all(self, *, enabled_only: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM mcp_sources"
        if enabled_only:
            sql += " WHERE enabled = true"
        sql += " ORDER BY name"
        rows = self.conn.execute(sql).fetchall()
        if not rows:
            return []
        cols = [d[0] for d in self.conn.description]
        return [self._row_to_dict(r, cols) for r in rows]

    def delete(self, source_id: str) -> None:
        self.conn.execute("DELETE FROM mcp_sources WHERE id = ?", [source_id])
