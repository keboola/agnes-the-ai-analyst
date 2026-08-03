"""add file_corpora.origin (uploaded | generated)

Mirrors DuckDB ``_v108_to_v109``. ``origin`` records artefact provenance for
the Artefacts toolbar's Source facet: every existing artefact is
user-uploaded, so the column defaults to ``'uploaded'``; the future
agent-generated-artefact writer sets ``'generated'``.

Idempotent + guarded so it's a no-op where the column already exists.

Revision ID: 0049_file_corpora_origin_v102
Revises: 0048_mcp_connect_hint_heal_v101
Create Date: 2026-07-28

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0049_file_corpora_origin_v102"
down_revision: Union[str, None] = "0045_user_journey_state_v98"  # restacked onto main head
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "file_corpora" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("file_corpora")}
    if "origin" not in cols:
        op.add_column(
            "file_corpora",
            sa.Column("origin", sa.String(), nullable=False, server_default="uploaded"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "file_corpora" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("file_corpora")}
    if "origin" in cols:
        op.drop_column("file_corpora", "origin")
