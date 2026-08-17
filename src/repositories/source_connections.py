"""Repository for `source_connections` (v119) — named data-source connections.

Spec: docs/superpowers/specs/2026-06-12-named-source-connections-design.md.
`config` is stored as a JSON string and returned as a dict. `is_default`
is unique per source_type — enforced here (both backends), not by the DB.

`slug` (extract directory name) and `alias` (DuckDB ATTACH alias) are
unique per row and immutable. Callers may omit them at create time; the
repository derives safe, unique values from the connection name.
"""

from __future__ import annotations

import builtins
import json
import re
from typing import Any

import duckdb

_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


def _safe_identifier(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", name.lower())
    s = re.sub(r"^[0-9]+", "", s)
    s = s.strip("_") or "conn"
    return s[:63]


class SourceConnectionsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row: Any, cols: list) -> dict[str, Any] | None:
        if not row:
            return None
        d = dict(zip(cols, row))
        if isinstance(d.get("config"), str):
            try:
                d["config"] = json.loads(d["config"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def _fetch_one(self, sql: str, params: list) -> dict[str, Any] | None:
        row = self.conn.execute(sql, params).fetchone()
        cols = [d[0] for d in self.conn.description] if row else []
        return self._row_to_dict(row, cols)

    def _taken_slug_aliases(self, exclude_id: str | None = None) -> tuple[set[str], set[str]]:
        rows = self.conn.execute("SELECT id, slug, alias FROM source_connections").fetchall()
        slugs = {r[1] for r in rows if r[1] and r[0] != exclude_id}
        aliases = {r[2] for r in rows if r[2] and r[0] != exclude_id}
        return slugs, aliases

    def _derive_slug_alias(
        self,
        id: str,
        name: str,
        source_type: str,
        is_default: bool,
        slug: str | None = None,
        alias: str | None = None,
    ) -> tuple[str, str]:
        # The default Keboola connection keeps the legacy names for backwards
        # compatibility; other rows derive from the connection name.
        if is_default and source_type == "keboola" and not slug and not alias:
            slug = "keboola"
            alias = "kbc"

        slug = (slug or _safe_identifier(name)).strip()
        alias = (alias or _safe_identifier(name)).strip()

        if not _SAFE_IDENTIFIER.match(slug):
            slug = "conn"
        if not _SAFE_IDENTIFIER.match(alias):
            alias = "conn"

        taken_slugs, taken_aliases = self._taken_slug_aliases()

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
        derived_slug, derived_alias = self._derive_slug_alias(id, name, source_type, is_default, slug, alias)
        # Wrap the default-demotion UPDATE + INSERT in one transaction so a
        # mid-way failure can't leave the old default demoted with no new row
        # inserted — matches the PG sibling's engine.begin() atomicity.
        self.conn.execute("BEGIN")
        try:
            if is_default:
                self.conn.execute(
                    "UPDATE source_connections SET is_default = FALSE WHERE source_type = ?",
                    [source_type],
                )
            self.conn.execute(
                """INSERT INTO source_connections
                   (id, name, slug, alias, source_type, config, token_env, is_default, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    id,
                    name,
                    derived_slug,
                    derived_alias,
                    source_type,
                    json.dumps(config),
                    token_env,
                    is_default,
                    created_by,
                ],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def get(self, connection_id: str) -> dict[str, Any] | None:
        return self._fetch_one("SELECT * FROM source_connections WHERE id = ?", [connection_id])

    def get_by_name(self, name: str) -> dict[str, Any] | None:
        return self._fetch_one("SELECT * FROM source_connections WHERE name = ?", [name])

    def get_by_slug(self, slug: str) -> dict[str, Any] | None:
        return self._fetch_one("SELECT * FROM source_connections WHERE slug = ?", [slug])

    def get_by_alias(self, alias: str) -> dict[str, Any] | None:
        return self._fetch_one("SELECT * FROM source_connections WHERE alias = ?", [alias])

    def get_default(self, source_type: str) -> dict[str, Any] | None:
        return self._fetch_one(
            "SELECT * FROM source_connections WHERE source_type = ? AND is_default ORDER BY created_at LIMIT 1",
            [source_type],
        )

    def list(self, source_type: str | None = None) -> builtins.list[dict[str, Any]]:
        if source_type:
            rows = self.conn.execute(
                "SELECT * FROM source_connections WHERE source_type = ? ORDER BY name",
                [source_type],
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM source_connections ORDER BY name").fetchall()
        cols = [d[0] for d in self.conn.description]
        return [self._row_to_dict(r, cols) for r in rows]  # type: ignore[misc]

    def update(
        self,
        connection_id: str,
        *,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        token_env: str | None = None,
        is_default: bool | None = None,
    ) -> None:
        # Atomic multi-column update — same transaction guarantee as the PG
        # sibling, so a failure between the UPDATEs can't half-apply.
        # `name` backs the "Add data source" wizard's post-test rename
        # (#755): the project name is only known after a successful
        # test-connection call, which requires the row to already exist, so
        # rename-after-create is the only way to honour the UX contract
        # without introducing a create-without-persisting endpoint.
        self.conn.execute("BEGIN")
        try:
            if name is not None:
                self.conn.execute(
                    "UPDATE source_connections SET name = ? WHERE id = ?",
                    [name, connection_id],
                )
            if config is not None:
                self.conn.execute(
                    "UPDATE source_connections SET config = ? WHERE id = ?",
                    [json.dumps(config), connection_id],
                )
            if token_env is not None:
                self.conn.execute(
                    "UPDATE source_connections SET token_env = ? WHERE id = ?",
                    [token_env, connection_id],
                )
            if is_default is not None:
                if is_default:
                    # Promote: demote every other connection of the same
                    # source_type first (is_default is unique per source_type,
                    # enforced here — mirrors create()).
                    row = self.conn.execute(
                        "SELECT source_type FROM source_connections WHERE id = ?",
                        [connection_id],
                    ).fetchone()
                    if row:
                        self.conn.execute(
                            "UPDATE source_connections SET is_default = FALSE WHERE source_type = ?",
                            [row[0]],
                        )
                    self.conn.execute(
                        "UPDATE source_connections SET is_default = TRUE WHERE id = ?",
                        [connection_id],
                    )
                else:
                    self.conn.execute(
                        "UPDATE source_connections SET is_default = FALSE WHERE id = ?",
                        [connection_id],
                    )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def delete(self, connection_id: str) -> None:
        self.conn.execute("DELETE FROM source_connections WHERE id = ?", [connection_id])
