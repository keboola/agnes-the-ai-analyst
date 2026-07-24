"""corpus_files.path — logical path for upsert-on-upload

Mirrors DuckDB ``_v96_to_v97``. Adds an optional caller-supplied identity
(e.g. a repo-relative path) to ``corpus_files`` so re-uploading the same
logical file REPLACES the existing row instead of inserting a duplicate
(keyed on ``(corpus_id, path)``). Nullable; NULL on every existing row and
on uploads that omit it (plain-insert behavior unchanged).

Revision ID: 0044_corpus_files_path_v97
Revises: 0043_data_apps_v96
Create Date: 2026-07-23

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0044_corpus_files_path_v97"
down_revision: Union[str, None] = "0043_data_apps_v96"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("corpus_files", sa.Column("path", sa.String(), nullable=True))
    # At most one row per (corpus_id, path). Plain unique index (not partial):
    # NULLs are distinct on both PG and DuckDB, so path=NULL rows are exempt
    # while set paths stay unique. Matches the DuckDB `_v96_to_v97` index.
    op.create_index(
        "idx_corpus_files_corpus_path",
        "corpus_files",
        ["corpus_id", "path"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("idx_corpus_files_corpus_path", table_name="corpus_files")
    op.drop_column("corpus_files", "path")
