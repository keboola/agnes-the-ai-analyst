"""instance_templates source_mode/git_path/base_sha (DuckDB v75 parity).

Adds an explicit Git⇄Editor source toggle to the per-key managed-prompt
store, superseding the implicit ``seed_owns()`` read-only lock. Existing
keys default to ``'editor'`` (the DB override wins at render time when set).
``base_sha`` is reserved for Slice 2 divergence detection (written, not read
in Slice 1).

Mirrors DuckDB ``_v74_to_v75``. Additive-only; the ``source_mode``
server_default keeps every existing row at ``'editor'``.

Revision ID: 0022_prompt_source_mode_v75
Revises: 0021_server_only_v74
Create Date: 2026-06-12
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022_prompt_source_mode_v75"
down_revision: Union[str, None] = "0021_server_only_v74"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "instance_templates",
        sa.Column(
            "source_mode",
            sa.String(),
            server_default=sa.text("'editor'"),
            nullable=False,
        ),
    )
    op.add_column(
        "instance_templates",
        sa.Column("git_path", sa.String(), nullable=True),
    )
    op.add_column(
        "instance_templates",
        sa.Column("base_sha", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("instance_templates", "base_sha")
    op.drop_column("instance_templates", "git_path")
    op.drop_column("instance_templates", "source_mode")
