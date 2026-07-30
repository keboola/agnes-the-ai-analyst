"""data_apps linked columns

Mirrors DuckDB ``_v107_to_v108``. Adds the columns a ``repo_mode='linked'``
(externally-hosted) data app needs — an external deployment URL, ingest
provenance, a sync-owned flag, and an admin description override — so apps
hosted elsewhere (e.g. Keboola-platform apps ingested via an MCP source) can be
surfaced as grantable resources without a git repo / runtime container.

Revision ID: 0055_dataapps_linked_v108
Revises: 0054_semantic_source_ref_v107
Create Date: 2026-07-29

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0055_dataapps_linked_v108"
down_revision: Union[str, None] = "0054_semantic_source_ref_v107"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("data_apps", sa.Column("external_url", sa.String(), nullable=True))
    op.add_column("data_apps", sa.Column("source_ref", sa.String(), nullable=True))
    op.add_column(
        "data_apps",
        sa.Column("managed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("data_apps", sa.Column("description_override", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("data_apps", "description_override")
    op.drop_column("data_apps", "managed")
    op.drop_column("data_apps", "source_ref")
    op.drop_column("data_apps", "external_url")
