"""Postgres-backed MCPSourceRepository.

Mirrors ``src/repositories/mcp_sources.py``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class MCPSourcePgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    @staticmethod
    def _decode_row(row: Dict[str, Any]) -> Dict[str, Any]:
        d = dict(row)
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
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO mcp_sources
                       (id, name, transport, command, args, env, url, auth_method,
                        auth_secret_env, enabled, scope, created_at, updated_at)
                       VALUES (:id, :name, :transport, :command, :args, :env, :url,
                               :auth_method, :auth_secret_env, :enabled, :scope,
                               :now, :now)
                       ON CONFLICT (id) DO UPDATE SET
                           name            = EXCLUDED.name,
                           transport       = EXCLUDED.transport,
                           command         = EXCLUDED.command,
                           args            = EXCLUDED.args,
                           env             = EXCLUDED.env,
                           url             = EXCLUDED.url,
                           auth_method     = EXCLUDED.auth_method,
                           auth_secret_env = EXCLUDED.auth_secret_env,
                           enabled         = EXCLUDED.enabled,
                           scope           = EXCLUDED.scope,
                           updated_at      = EXCLUDED.updated_at"""
                ),
                {
                    "id": id, "name": name, "transport": transport,
                    "command": command, "args": args_json, "env": env_json, "url": url,
                    "auth_method": auth_method, "auth_secret_env": auth_secret_env,
                    "enabled": enabled, "scope": scope, "now": now,
                },
            )

    def get(self, source_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM mcp_sources WHERE id = :id"),
                {"id": source_id},
            ).mappings().first()
        return self._decode_row(dict(row)) if row else None

    def get_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM mcp_sources WHERE name = :name"),
                {"name": name},
            ).mappings().first()
        return self._decode_row(dict(row)) if row else None

    def list_all(self, *, enabled_only: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM mcp_sources"
        if enabled_only:
            sql += " WHERE enabled = true"
        sql += " ORDER BY name"
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql)).mappings().all()
        return [self._decode_row(dict(r)) for r in rows]

    def delete(self, source_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM mcp_sources WHERE id = :id"),
                {"id": source_id},
            )
