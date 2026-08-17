"""tool_registry.projection_map — which columns carry a linked app's id/url/name

Mirrors DuckDB ``_v118_to_v119``.

The linked-apps projection asked a hardcoded alias list which materialized
column held an app's id (``id``/``app_id``/``config_id``) and which its URL.
The list was written against one upstream; a server naming its columns
differently has every row silently skipped, and the projection reports
"0 new, 0 updated" — which reads as "the upstream has nothing" rather than
"nothing here is named what I expected". Live example: a Keboola data-app
lister emits ``data_app_id`` + ``configuration_id``, so all six rows were
dropped while the wizard called the fetch a success.

Per-tool because the mapping describes THAT tool's output shape. NULL keeps
the old alias behaviour, so no instance changes until an admin chooses.

Revision ID: 0067_tool_projection_map_v119
Revises: 0066_metric_grain_nullable
Create Date: 2026-08-17
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0067_tool_projection_map_v119"
down_revision: Union[str, None] = "0066_metric_grain_nullable"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "tool_registry" not in set(insp.get_table_names()):
        return
    existing_cols = {col["name"] for col in insp.get_columns("tool_registry")}
    if "projection_map" in existing_cols:
        return
    op.add_column(
        "tool_registry",
        sa.Column("projection_map", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "tool_registry" not in set(insp.get_table_names()):
        return
    existing_cols = {col["name"] for col in insp.get_columns("tool_registry")}
    if "projection_map" not in existing_cols:
        return
    op.drop_column("tool_registry", "projection_map")
