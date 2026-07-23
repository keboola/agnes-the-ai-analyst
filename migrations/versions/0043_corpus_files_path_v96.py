"""corpus_files.path — logical path for upsert-on-upload

Mirrors DuckDB ``_v95_to_v96``. Adds an optional caller-supplied identity
(e.g. a repo-relative path) to ``corpus_files`` so re-uploading the same
logical file REPLACES the existing row instead of inserting a duplicate
(keyed on ``(corpus_id, path)``). Nullable; NULL on every existing row and
on uploads that omit it (plain-insert behavior unchanged).

Revision ID: 0043_corpus_files_path_v96
Revises: 0042_usage_summary_idx_fix_v95
Create Date: 2026-07-23

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0043_corpus_files_path_v96"
down_revision: Union[str, None] = "0042_usage_summary_idx_fix_v95"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("corpus_files", sa.Column("path", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("corpus_files", "path")
