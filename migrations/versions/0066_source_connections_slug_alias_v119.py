"""source_connections.slug/alias — per-connection Keboola extracts

Mirrors DuckDB ``_v118_to_v119``. Adds ``slug`` and ``alias`` to
``source_connections`` and backfills existing rows with safe, unique values.
The default Keboola connection keeps the legacy ``slug='keboola'`` and
``alias='kbc'``.

Revision ID: 0066_source_conn_slug_alias_v119
Revises: 0065_journey_agent_created_v118
Create Date: 2026-08-17
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066_source_conn_slug_alias_v119"
down_revision: str | None = "0066_metric_grain_nullable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


def _safe_identifier(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", name.lower())
    s = re.sub(r"^[0-9]+", "", s)
    s = s.strip("_") or "conn"
    return s[:63]


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "source_connections" not in set(insp.get_table_names()):
        return

    existing_cols = {col["name"] for col in insp.get_columns("source_connections")}
    if "slug" not in existing_cols:
        op.add_column("source_connections", sa.Column("slug", sa.String(), nullable=True))
    if "alias" not in existing_cols:
        op.add_column("source_connections", sa.Column("alias", sa.String(), nullable=True))

    rows = bind.execute(sa.text("SELECT id, name, source_type, is_default FROM source_connections")).fetchall()

    taken_slugs: set[str] = set()
    taken_aliases: set[str] = set()
    updates: list[tuple[str, str, str]] = []

    for row_id, name, source_type, is_default in rows:
        if source_type == "keboola" and is_default:
            updates.append(("keboola", "kbc", row_id))
            taken_slugs.add("keboola")
            taken_aliases.add("kbc")

    for row_id, name, source_type, is_default in rows:
        if source_type == "keboola" and is_default:
            continue

        slug = _safe_identifier(name)
        alias = _safe_identifier(name)
        if not _SAFE_IDENTIFIER.match(slug):
            slug = "conn"
        if not _SAFE_IDENTIFIER.match(alias):
            alias = "conn"

        base_slug = slug
        counter = 1
        while slug in taken_slugs:
            slug = f"{base_slug}_{counter}"[:63]
            counter += 1

        base_alias = alias
        counter = 1
        while alias in taken_aliases:
            alias = f"{base_alias}_{counter}"[:63]
            counter += 1

        if slug in taken_slugs:
            slug = f"{base_slug}_{row_id[:8]}"[:63]
        if alias in taken_aliases:
            alias = f"{base_alias}_{row_id[:8]}"[:63]

        updates.append((slug, alias, row_id))
        taken_slugs.add(slug)
        taken_aliases.add(alias)

    for slug, alias, row_id in updates:
        bind.execute(
            sa.text("UPDATE source_connections SET slug = :slug, alias = :alias WHERE id = :id"),
            {"slug": slug, "alias": alias, "id": row_id},
        )

    op.alter_column("source_connections", "slug", nullable=False)
    op.alter_column("source_connections", "alias", nullable=False)
    op.create_unique_constraint("uq_source_connections_slug", "source_connections", ["slug"])
    op.create_unique_constraint("uq_source_connections_alias", "source_connections", ["alias"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "source_connections" not in set(insp.get_table_names()):
        return
    existing_cols = {col["name"] for col in insp.get_columns("source_connections")}
    if "slug" in existing_cols:
        op.drop_column("source_connections", "slug")
    if "alias" in existing_cols:
        op.drop_column("source_connections", "alias")
