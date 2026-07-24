"""data_apps draft model — parent_app_id, is_draft, draft_branch

Mirrors DuckDB ``_v97_to_v98``. Adds the columns that let a data app have
draft copies: ``parent_app_id`` points a draft row back at its production
app, ``is_draft`` flags the row as a draft (excluded from ``list()`` when
``include_drafts=False``), and ``draft_branch`` records the git branch the
draft was built from.

Revision ID: 0045_data_apps_drafts_v98
Revises: 0044_corpus_files_path_v97
Create Date: 2026-07-24

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0045_data_apps_drafts_v98"
down_revision: Union[str, None] = "0044_corpus_files_path_v97"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("data_apps", sa.Column("parent_app_id", sa.String(), server_default=""))
    op.add_column("data_apps", sa.Column("is_draft", sa.Boolean(), server_default=sa.text("false")))
    op.add_column("data_apps", sa.Column("draft_branch", sa.String(), server_default=""))


def downgrade() -> None:
    op.drop_column("data_apps", "draft_branch")
    op.drop_column("data_apps", "is_draft")
    op.drop_column("data_apps", "parent_app_id")
