"""Postgres-backed SourceConnectionsRepository.

Mirrors ``src/repositories/source_connections.py``.
"""

from __future__ import annotations

import builtins
import json
import re
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


def _safe_identifier(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", name.lower())
    s = re.sub(r"^[0-9]+", "", s)
    s = s.strip("_") or "conn"
    return s[:63]


class SourceConnectionsPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    @staticmethod
    def _decode(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        if isinstance(d.get("config"), str):
            try:
                d["config"] = json.loads(d["config"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def _taken_slug_aliases(self, cx: Any, exclude_id: str | None = None) -> tuple[set[str], set[str]]:
        if exclude_id:
            rows = (
                cx.execute(
                    sa.text("SELECT id, slug, alias FROM source_connections WHERE id != :ex"),
                    {"ex": exclude_id},
                )
                .mappings()
                .fetchall()
            )
        else:
            rows = cx.execute(sa.text("SELECT id, slug, alias FROM source_connections")).mappings().fetchall()
        slugs = {r["slug"] for r in rows if r["slug"]}
        aliases = {r["alias"] for r in rows if r["alias"]}
        return slugs, aliases

    def _derive_slug_alias(
        self,
        cx: Any,
        id: str,
        name: str,
        source_type: str,
        is_default: bool,
        slug: str | None = None,
        alias: str | None = None,
    ) -> tuple[str, str]:
        if is_default and source_type == "keboola" and not slug and not alias:
            slug = "keboola"
            alias = "kbc"

        slug = (slug or _safe_identifier(name)).strip()
        alias = (alias or _safe_identifier(name)).strip()

        if not _SAFE_IDENTIFIER.match(slug):
            slug = "conn"
        if not _SAFE_IDENTIFIER.match(alias):
            alias = "conn"

        taken_slugs, taken_aliases = self._taken_slug_aliases(cx, exclude_id=id)

        base_slug = slug
        counter = 1
        while slug in taken_slugs:
            slug = f"{base_slug}_{counter}"[:63]
            counter += 1
        if slug in taken_slugs:
            slug = f"{base_slug}_{id[:8]}"[:63]

        base_alias = alias
        counter = 1
        while alias in taken_aliases:
            alias = f"{base_alias}_{counter}"[:63]
            counter += 1
        if alias in taken_aliases:
            alias = f"{base_alias}_{id[:8]}"[:63]

        return slug, alias

    def create(
        self,
        *,
        id: str,
        name: str,
        source_type: str,
        config: dict[str, Any],
        token_env: str | None = None,
        is_default: bool = False,
        created_by: str | None = None,
        slug: str | None = None,
        alias: str | None = None,
    ) -> None:
        with self._engine.begin() as cx:
            derived_slug, derived_alias = self._derive_slug_alias(cx, id, name, source_type, is_default, slug, alias)
            if is_default:
                cx.execute(
                    sa.text("UPDATE source_connections SET is_default = FALSE WHERE source_type = :st"),
                    {"st": source_type},
                )
            cx.execute(
                sa.text(
                    """INSERT INTO source_connections
                       (id, name, slug, alias, source_type, config, token_env, is_default, created_by)
                       VALUES (:id, :name, :slug, :alias, :st, :config, :token_env, :is_default, :created_by)"""
                ),
                {
                    "id": id,
                    "name": name,
                    "slug": derived_slug,
                    "alias": derived_alias,
                    "st": source_type,
                    "config": json.dumps(config),
                    "token_env": token_env,
                    "is_default": is_default,
                    "created_by": created_by,
                },
            )

    def _fetch_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        with self._engine.connect() as cx:
            row = cx.execute(sa.text(sql), params).mappings().fetchone()
        return self._decode(dict(row) if row else None)

    def get(self, connection_id: str) -> dict[str, Any] | None:
        return self._fetch_one("SELECT * FROM source_connections WHERE id = :id", {"id": connection_id})

    def get_by_name(self, name: str) -> dict[str, Any] | None:
        return self._fetch_one("SELECT * FROM source_connections WHERE name = :n", {"n": name})

    def get_by_slug(self, slug: str) -> dict[str, Any] | None:
        return self._fetch_one("SELECT * FROM source_connections WHERE slug = :s", {"s": slug})

    def get_by_alias(self, alias: str) -> dict[str, Any] | None:
        return self._fetch_one("SELECT * FROM source_connections WHERE alias = :a", {"a": alias})

    def get_default(self, source_type: str) -> dict[str, Any] | None:
        return self._fetch_one(
            "SELECT * FROM source_connections WHERE source_type = :st AND is_default ORDER BY created_at LIMIT 1",
            {"st": source_type},
        )

    def list(self, source_type: str | None = None) -> builtins.list[dict[str, Any]]:
        sql = "SELECT * FROM source_connections"
        params: dict[str, Any] = {}
        if source_type:
            sql += " WHERE source_type = :st"
            params["st"] = source_type
        sql += " ORDER BY name"
        with self._engine.connect() as cx:
            rows = cx.execute(sa.text(sql), params).mappings().fetchall()
        return [self._decode(dict(r)) for r in rows]  # type: ignore[misc]

    def update(
        self,
        connection_id: str,
        *,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        token_env: str | None = None,
        is_default: bool | None = None,
    ) -> None:
        # `name` backs the "Add data source" wizard's post-test rename
        # (#755) — see the DuckDB sibling's docstring for the rationale.
        with self._engine.begin() as cx:
            if name is not None:
                cx.execute(
                    sa.text("UPDATE source_connections SET name = :n WHERE id = :id"),
                    {"n": name, "id": connection_id},
                )
            if config is not None:
                cx.execute(
                    sa.text("UPDATE source_connections SET config = :c WHERE id = :id"),
                    {"c": json.dumps(config), "id": connection_id},
                )
            if token_env is not None:
                cx.execute(
                    sa.text("UPDATE source_connections SET token_env = :t WHERE id = :id"),
                    {"t": token_env, "id": connection_id},
                )
            if is_default is not None:
                if is_default:
                    # Promote: demote every other connection of the same
                    # source_type first (is_default is unique per source_type,
                    # enforced here — mirrors create()).
                    row = (
                        cx.execute(
                            sa.text("SELECT source_type FROM source_connections WHERE id = :id"),
                            {"id": connection_id},
                        )
                        .mappings()
                        .fetchone()
                    )
                    if row:
                        cx.execute(
                            sa.text("UPDATE source_connections SET is_default = FALSE WHERE source_type = :st"),
                            {"st": row["source_type"]},
                        )
                    cx.execute(
                        sa.text("UPDATE source_connections SET is_default = TRUE WHERE id = :id"),
                        {"id": connection_id},
                    )
                else:
                    cx.execute(
                        sa.text("UPDATE source_connections SET is_default = FALSE WHERE id = :id"),
                        {"id": connection_id},
                    )

    def delete(self, connection_id: str) -> None:
        with self._engine.begin() as cx:
            cx.execute(
                sa.text("DELETE FROM source_connections WHERE id = :id"),
                {"id": connection_id},
            )
